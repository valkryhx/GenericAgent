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
}

export function formatTokenCount(tokens: number): string {
  const safeTokens = Math.max(0, Math.round(tokens))
  if (safeTokens < 1000) return String(safeTokens)
  const value = safeTokens / 1000
  return `${Number.isInteger(value) ? String(value) : value.toFixed(1)}k`
}

export function formatTokenUsage(usage: TokenUsage): string {
  return `↑${formatTokenCount(usage.inputTokens)} ↓${formatTokenCount(usage.outputTokens)} Σ${formatTokenCount(usage.totalTokens)}`
}

export function formatRunningStatus(seconds: number, label = 'Hyperspacing', usage?: TokenUsage | null): string {
  const parts = [formatElapsed(seconds)]
  if (usage) parts.push(formatTokenUsage(usage))
  return `✻ ${label} (${parts.join(' · ')})`
}

export function shouldShowActivityStatus(
  status: 'connecting' | 'idle' | 'running' | 'stopping',
  hasRunningTimer: boolean,
  usage?: TokenUsage | null,
): boolean {
  if ((status === 'running' || status === 'stopping') && hasRunningTimer) return true
  return status === 'idle' && usage !== null && usage !== undefined
}
