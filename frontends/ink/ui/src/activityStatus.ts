export const RUNNING_VERBS = [
  'Accomplishing',
  'Architecting',
  'Bootstrapping',
  'Calculating',
  'Channeling',
  'Composing',
  'Computing',
  'Considering',
  'Cooking',
  'Crafting',
  'Crunching',
  'Deciphering',
  'Deliberating',
  'Generating',
  'Hashing',
  'Hyperspacing',
  'Imagining',
  'Inferring',
  'Manifesting',
  'Orchestrating',
  'Percolating',
  'Pondering',
  'Processing',
  'Ruminating',
  'Simmering',
  'Synthesizing',
  'Thinking',
  'Tinkering',
  'Transmuting',
  'Working',
  'Wrangling',
]

export function pickRunningVerb(random = Math.random): string {
  const index = Math.min(RUNNING_VERBS.length - 1, Math.floor(random() * RUNNING_VERBS.length))
  return RUNNING_VERBS[index] ?? 'Working'
}

export function formatElapsed(seconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(seconds))
  if (safeSeconds < 60) return `${safeSeconds}s`
  const minutes = Math.floor(safeSeconds / 60)
  const rest = safeSeconds % 60
  return `${minutes}m ${rest}s`
}

export type TokenUsage = {
  inputTokens: number
  outputTokens: number
  totalTokens: number
  cachedTokens?: number
  cacheReadTokens?: number
  cacheCreationTokens?: number
}

export function formatTokenCount(tokens: number): string {
  const safeTokens = Math.max(0, Math.round(tokens))
  if (safeTokens < 1000) return String(safeTokens)
  const value = safeTokens / 1000
  return `${Number.isInteger(value) ? String(value) : value.toFixed(1)}k`
}

export function formatTokenUsage(usage: TokenUsage): string {
  const parts = [`↑${formatTokenCount(usage.inputTokens)}`, `↓${formatTokenCount(usage.outputTokens)}`, `Σ${formatTokenCount(usage.totalTokens)}`]
  const cached = Math.max(0, usage.cacheReadTokens ?? usage.cachedTokens ?? 0)
  const creation = Math.max(0, usage.cacheCreationTokens ?? 0)
  if (cached > 0) parts.push(`cache ${formatTokenCount(cached)}`)
  if (creation > 0) parts.push(`write ${formatTokenCount(creation)}`)
  return parts.join(' ')
}

/**
 * 状态栏的一个片段。`emphasis` 的片段会被渲染成粗体。
 *
 * 为什么要拆段而不是给整行加粗：状态栏一行里既有「✓ Hyperspacing (40s)」这种
 * 辅助信息，也有 ↑输入 ↓输出 Σ总和 cache 这种用户真正盯着的数字。整行加粗会让
 * 数字和说明文字糊成一片，反而更难扫读。
 */
export type StatusSegment = {
  text: string
  /** 渲染时加粗。用于 token 用量这类需要突出的数字。 */
  emphasis?: boolean
}

function tokenUsageSegment(usage: TokenUsage | null | undefined): StatusSegment[] {
  if (!usage) return []
  const text = formatTokenUsage(usage)
  return text.length > 0 ? [{ text, emphasis: true }] : []
}

/** 运行中状态，按片段返回（供渲染层分别着色/加粗）。 */
export function runningStatusSegments(seconds: number, label = 'Hyperspacing', usage?: TokenUsage | null): StatusSegment[] {
  const head = `✻ ${label} (${formatElapsed(seconds)}`
  const segments: StatusSegment[] = [{ text: head }]
  const tokens = tokenUsageSegment(usage)
  if (tokens.length > 0) {
    segments.push({ text: ' · ' })
    segments.push(...tokens)
  }
  segments.push({ text: ')' })
  return segments
}

/** 已完成状态，按片段返回。 */
export function completedStatusSegments(seconds: number, completedAt: Date, usage?: TokenUsage | null): StatusSegment[] {
  const safeSeconds = Math.max(0, Math.floor(seconds))
  const hours = Math.floor(safeSeconds / 3600)
  const minutes = Math.floor((safeSeconds % 3600) / 60)
  const rest = safeSeconds % 60
  const elapsed = hours > 0
    ? `${hours}h ${minutes}m ${rest}s`
    : minutes > 0
      ? `${minutes}m ${rest}s`
      : rest > 0 ? `${rest}s` : '<1s'
  const date = completedAt.toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
  const time = `${String(completedAt.getHours()).padStart(2, '0')}:${String(completedAt.getMinutes()).padStart(2, '0')}`
  const segments: StatusSegment[] = [{ text: `Worked for ${elapsed} • ${date} at ${time}` }]
  const tokens = tokenUsageSegment(usage)
  if (tokens.length > 0) {
    segments.push({ text: ' • ' })
    segments.push(...tokens)
  }
  return segments
}

export function formatRunningStatus(seconds: number, label = 'Hyperspacing', usage?: TokenUsage | null): string {
  return runningStatusSegments(seconds, label, usage).map(segment => segment.text).join('')
}

export function formatCompletedStatus(seconds: number, completedAt: Date, usage?: TokenUsage | null): string {
  return completedStatusSegments(seconds, completedAt, usage).map(segment => segment.text).join('')
}

/**
 * CC 的重试横幅文案（m1795）：`· Retrying in 3s · attempt 1/10`。
 *
 * GA 这里多带一个状态码，因为 zhanlu-ds 这类网关的 502 和传输层超时在用户眼里
 * 是两件事（"服务端 502" vs "网络断了"）。`status === 0` 表示没拿到 HTTP 响应。
 */
export function retryStatusText(status: number, secondsLeft: number, attempt: number, maxRetries: number): string {
  const reason = status > 0 ? ` (HTTP ${status})` : ''
  const total = Math.max(attempt, maxRetries)
  return `· Retrying in ${formatElapsed(secondsLeft)}${reason} · attempt ${attempt}/${total} · esc to interrupt`
}

export function shouldShowActivityStatus(
  status: 'connecting' | 'idle' | 'running' | 'stopping',
  hasRunningTimer: boolean,
  usage?: TokenUsage | null,
  hasCompletedTurn = false,
): boolean {
  if ((status === 'running' || status === 'stopping') && hasRunningTimer) return true
  return status === 'idle' && (hasCompletedTurn || (usage !== null && usage !== undefined))
}
