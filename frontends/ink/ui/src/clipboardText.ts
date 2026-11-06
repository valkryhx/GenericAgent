/**
 * 读系统剪贴板「文本」。用于 Ctrl+V 贴图失败时的回退（对齐 CC 的
 * imagePaste 失败 → D() 读文本，见 docs/20261024-GA-输入框看图优化设计与实施方案.md O1）。
 *
 * 只读文本，不碰图片；任何失败都返回空串，绝不让粘贴快捷键抛错。
 */
import { spawnSync } from 'node:child_process'

const DEFAULT_TIMEOUT_MS = 2000

function readWith(cmd: string, args: string[], timeoutMs: number): string {
  try {
    const r = spawnSync(cmd, args, { encoding: 'utf-8', timeout: timeoutMs })
    if (r.error || r.status !== 0) return ''
    return (r.stdout || '').replace(/\r\n/g, '\n')
  } catch {
    return ''
  }
}

/**
 * 返回剪贴板文本（保留内部换行，统一成 LF）；无文本/无工具/超时一律返回 ''。
 * 可用 GA_CLIPBOARD_TEXT_TIMEOUT_MS 覆盖超时（毫秒）。
 */
export function readClipboardText(timeoutMs: number = resolveTimeout()): string {
  if (process.env.GA_CLIPBOARD_TEXT_FALLBACK === '0') return ''
  if (process.platform === 'win32') {
    return readWith('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', 'Get-Clipboard -Raw'], timeoutMs)
  }
  if (process.platform === 'darwin') {
    return readWith('pbpaste', [], timeoutMs)
  }
  return readWith(
    'bash',
    ['-lc', 'wl-paste --no-newline 2>/dev/null || xclip -selection clipboard -o 2>/dev/null'],
    timeoutMs,
  )
}

function resolveTimeout(): number {
  const raw = Number(process.env.GA_CLIPBOARD_TEXT_TIMEOUT_MS)
  return Number.isFinite(raw) && raw > 0 ? raw : DEFAULT_TIMEOUT_MS
}
