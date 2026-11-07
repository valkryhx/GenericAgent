/**
 * 系统剪贴板「文本」读写。
 *
 * 读：用于 Ctrl+V 贴图失败时的回退（对齐 CC 的 imagePaste 失败 → D() 读文本，
 * 见 docs/20261024-GA-输入框看图优化设计与实施方案.md O1）。
 * 写：用于应用内选区复制 —— GA 打开鼠标捕获后终端的原生选中被关掉，
 * 必须自己把选中文本放进剪贴板（CC 的做法相同，见 transcriptSelection.ts）。
 *
 * 只处理文本，不碰图片；任何失败都安静返回，绝不让快捷键抛错。
 */
import { spawn, spawnSync } from 'node:child_process'

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

const OSC52_LIMIT_BYTES = 100_000

/**
 * OSC 52 写剪贴板。文本走 base64，终端负责落到系统剪贴板；在 tmux / ssh /
 * 远程会话里这是**唯一**能用的路径（本地 powershell/xclip 会去改错的机器）。
 * 超过常见终端上限（~100 KB）时返回 false，让调用方回退到本地工具。
 */
export function osc52ClipboardSequence(text: string): string | null {
  const payload = Buffer.from(text, 'utf-8').toString('base64')
  if (payload.length > OSC52_LIMIT_BYTES) return null
  return `\u001B]52;c;${payload}\u0007`
}

/**
 * 是否运行在远端会话里（SSH / tmux / screen）。
 *
 * 这个判定决定走哪条剪贴板通路，且必须保守：
 * - 本地：直接用平台工具（powershell Set-Clipboard / pbcopy / wl-copy），
 *   能在本机验证结果，且 Windows Terminal 默认**不认** OSC 52。
 * - 远端：本地进程改的是远端的剪贴板，用户拿不到，只能靠 OSC 52 让终端去写。
 */
function isRemoteSession(env: Record<string, string | undefined> = process.env): boolean {
  if (env.GA_CLIPBOARD_REMOTE === '1') return true
  if (env.GA_CLIPBOARD_REMOTE === '0') return false
  return Boolean(env.SSH_TTY || env.SSH_CONNECTION || env.TMUX || env.STY)
}

/**
 * 把文本写进系统剪贴板。
 *
 * 顺序按「哪种方式能确认成功」排：
 * 1. 远端会话 → OSC 52（唯一能让**用户那台**终端写剪贴板的方式）；
 * 2. 本地 → 平台工具，且检查退出码；
 * 3. 平台工具不可用 → 再退回 OSC 52（对支持它的终端比如 Windows Terminal 1.22+、
 *    kitty、iTerm2 有效；不支持就被终端丢弃，我们无法察觉）。
 *
 * 不用 OSC 52 直接成功来短路，就是因为终端会静默丢弃它：写出去不等于生效，
 * 而用户看到的提示必须是可信的。
 */
export function writeClipboardText(
  text: string,
  stdout: Pick<NodeJS.WriteStream, 'write'>,
  timeoutMs: number = resolveTimeout(),
): boolean {
  if (text.length === 0) return false
  if (process.env.GA_CLIPBOARD_WRITE === '0') return false

  const remote = isRemoteSession()
  if (remote) {
    const osc = osc52ClipboardSequence(text)
    if (osc) {
      try {
        stdout.write(osc)
        return true
      } catch {
        return false
      }
    }
    return false
  }

  if (writeWithPlatformTool(text, timeoutMs)) return true

  const osc = osc52ClipboardSequence(text)
  if (osc) {
    try {
      stdout.write(osc)
      return true
    } catch {
      return false
    }
  }
  return false
}

/**
 * Windows 写剪贴板的命令行。
 *
 * 不能把文本直接经 stdin 交给 `Set-Clipboard -Value $input`：PowerShell 按
 * **控制台代码页**（本机 GBK）解码 stdin，UTF-8 的中文会变成 `锟斤拷`。
 * 所以先 base64（纯 ASCII），在 PowerShell 里按 UTF-8 解码再写剪贴板。
 */
function windowsSetClipboardCommand(): string {
  const script = [
    '$b=[Console]::In.ReadToEnd();',
    '$t=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($b));',
    'Set-Clipboard -Value $t',
  ].join(' ')
  return script
}

function windowsClipboardStdin(text: string): string {
  return Buffer.from(text, 'utf-8').toString('base64')
}

function writeWithPlatformTool(text: string, timeoutMs: number): boolean {
  try {
    if (process.platform === 'win32') {
      const r = spawnSync(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-Command', windowsSetClipboardCommand()],
        { input: windowsClipboardStdin(text), encoding: 'utf-8', timeout: timeoutMs },
      )
      if (r.error || r.status !== 0) return false
      return true
    }
    if (process.platform === 'darwin') {
      const r = spawnSync('pbcopy', [], { input: text, encoding: 'utf-8', timeout: timeoutMs })
      return !r.error && r.status === 0
    }
    const r = spawnSync(
      'bash',
      ['-lc', 'wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null'],
      { input: text, encoding: 'utf-8', timeout: timeoutMs },
    )
    return !r.error && r.status === 0
  } catch {
    return false
  }
}

/**
 * 写入但不阻塞渲染：平台工具那一支会拉起一个 powershell 进程（几百毫秒），
 * 不能等它。远端会话下 OSC 52 是唯一的真通路，立刻发出并报告成功。
 */
export function writeClipboardTextAsync(
  text: string,
  stdout: Pick<NodeJS.WriteStream, 'write'>,
  onResult?: (ok: boolean) => void,
): void {
  if (text.length === 0) {
    onResult?.(false)
    return
  }
  if (process.env.GA_CLIPBOARD_WRITE === '0') {
    onResult?.(false)
    return
  }

  if (isRemoteSession()) {
    const osc = osc52ClipboardSequence(text)
    if (osc) {
      try {
        stdout.write(osc)
        onResult?.(true)
        return
      } catch {
        onResult?.(false)
        return
      }
    }
    onResult?.(false)
    return
  }

  void (async () => {
    if (await writeWithPlatformToolAsync(text)) {
      onResult?.(true)
      return
    }
    const osc = osc52ClipboardSequence(text)
    if (!osc) {
      onResult?.(false)
      return
    }
    try {
      stdout.write(osc)
      onResult?.(true)
    } catch {
      onResult?.(false)
    }
  })()
}

function writeWithPlatformToolAsync(text: string): Promise<boolean> {
  return new Promise(resolve => {
    const finish = (ok: boolean) => resolve(ok)
    try {
      const command = process.platform === 'win32'
        ? { cmd: 'powershell.exe', args: ['-NoProfile', '-NonInteractive', '-Command', windowsSetClipboardCommand()] }
        : process.platform === 'darwin'
          ? { cmd: 'pbcopy', args: [] }
          : { cmd: 'bash', args: ['-lc', 'wl-copy 2>/dev/null || xclip -selection clipboard 2>/dev/null'] }
      const child = spawn(command.cmd, command.args, { stdio: ['pipe', 'ignore', 'ignore'] })
      const timer = setTimeout(() => {
        child.kill()
        finish(false)
      }, DEFAULT_TIMEOUT_MS)
      child.on('error', () => {
        clearTimeout(timer)
        finish(false)
      })
      child.on('close', code => {
        clearTimeout(timer)
        finish(code === 0)
      })
      child.stdin?.end(process.platform === 'win32' ? windowsClipboardStdin(text) : text)
    } catch {
      finish(false)
    }
  })
}
