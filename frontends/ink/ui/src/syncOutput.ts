/**
 * DEC 2026 同步输出（synchronized output）的探测与包装。
 *
 * 为什么需要它：ink@5.2.1 的 `log-update` 每帧写 `eraseLines(prevN) + output`，
 * 是一个「先把动态区擦空、再整块重画」的两段式动作。若这两段之间终端有一次屏幕刷新，
 * 用户就会看到动态区闪空一下 —— 光标看起来像「往下穿了一下 / 压到输入框下侧线」。
 * 实测这条链路从未使用过 BSU/ESU（`probes/cursor_toggle_cost.ts`：`2026h/l = 0 / 0`）。
 *
 * DEC 2026（BSU/ESU）让终端把一段写入当作事务：要么整块显示，要么整块不显示，
 * 中间态不可见。Claude Code 的 ink fork 就是这么做的
 * （`src/ink/terminal.ts:203 writeDiffToTerminal`）。
 *
 * 探测逻辑照搬 CC `src/ink/terminal.ts:70 isSynchronizedOutputSupported()`，
 * 并额外支持环境变量强制覆盖，便于在不支持的终端上排查。
 */

/** Begin Synchronized Update。 */
export const BSU = '\u001B[?2026h'
/** End Synchronized Update。 */
export const ESU = '\u001B[?2026l'

const FORCE_OFF_VALUES = new Set(['0', 'off', 'false', 'no'])
const FORCE_ON_VALUES = new Set(['1', 'on', 'true', 'yes'])

export type SyncOutputEnv = {
  platform?: NodeJS.Platform
  env?: NodeJS.ProcessEnv
}

/**
 * 终端是否支持 DEC 2026。
 *
 * 只读环境变量 + 平台，不碰 stdin（探测序列在 Windows Terminal 上有回显风险，
 * CC 也只用环境变量判定这一项）。
 *
 * `GA_UI_SYNC_OUTPUT` 可强制覆盖：
 *   - `0` / `off` / `false` / `no` → 关闭
 *   - `1` / `on`  / `true`  / `yes` → 开启
 * 未设置时按下面的探测表判定。
 */
export function isSynchronizedOutputSupported(input: SyncOutputEnv = {}): boolean {
  const env = input.env ?? process.env
  const platform = input.platform ?? process.platform

  const override = (env.GA_UI_SYNC_OUTPUT ?? '').trim().toLowerCase()
  if (FORCE_OFF_VALUES.has(override)) return false
  if (FORCE_ON_VALUES.has(override)) return true

  // tmux 会解析并转发每个字节，但不实现 DEC 2026 —— 它已经把原子性打碎了。
  // 包了只会多 16 字节/帧并带来误判，直接跳过。
  if (env.TMUX) return false

  const termProgram = env.TERM_PROGRAM
  const term = env.TERM ?? ''

  // 明确支持 DEC 2026 的终端。
  if (
    termProgram === 'iTerm.app' ||
    termProgram === 'WezTerm' ||
    termProgram === 'WarpTerminal' ||
    termProgram === 'ghostty' ||
    termProgram === 'contour' ||
    termProgram === 'vscode' ||
    termProgram === 'alacritty'
  ) {
    return true
  }

  // kitty 用 TERM=xterm-kitty 或 KITTY_WINDOW_ID。
  if (term.includes('kitty') || env.KITTY_WINDOW_ID) return true
  // Ghostty 可能只设 TERM=xterm-ghostty。
  if (term === 'xterm-ghostty') return true
  if (term.startsWith('foot')) return true
  if (term.includes('alacritty')) return true
  // Zed 用 alacritty_terminal，支持 DEC 2026。
  if (env.ZED_TERM) return true
  // Windows Terminal（WSL 里跑也一样，输出最终走 WT）。
  if (env.WT_SESSION) return true

  // VTE 系（GNOME Terminal、Tilix…）自 0.68 起支持。
  const vteVersion = env.VTE_VERSION
  if (vteVersion) {
    const version = Number.parseInt(vteVersion, 10)
    if (Number.isFinite(version) && version >= 6800) return true
  }

  // 老 conhost 不支持：显式排除，避免在 Windows 原生控制台上写出无人消费的序列。
  if (platform === 'win32' && !env.WT_SESSION && !env.TERM_PROGRAM) return false

  return false
}

/**
 * 把一次完整写入包进 BSU/ESU。
 *
 * `enabled` 为 false 时原样返回 —— 调用方因此可以用同一个代码路径，只切换这一个布尔。
 * 空串不包装（避免写出 `BSU+ESU` 这种没有内容的空事务）。
 */
export function wrapSynchronizedUpdate(chunk: string, enabled: boolean): string {
  if (!enabled || chunk.length === 0) return chunk
  return `${BSU}${chunk}${ESU}`
}
