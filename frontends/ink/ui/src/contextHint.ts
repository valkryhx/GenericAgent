/**
 * Format the context-usage warning for the hint line.
 *
 * The bridge emits one `context_usage` event per level crossing (including the
 * drop back to `ok` after compaction), so this is a pure formatter: `ok` maps to
 * null and the hint disappears.
 */
export type ContextLevel = 'ok' | 'warn' | 'compact' | 'blocked'

export type ContextUsage = {
  level: ContextLevel
  used_tokens: number
  limit_tokens: number
  hard_tokens?: number
}

export function contextUsageHint(usage: ContextUsage): string | null {
  if (!usage || usage.level === 'ok' || !usage.limit_tokens) return null
  const percent = Math.min(100, Math.max(0, Math.round((usage.used_tokens * 100) / usage.limit_tokens)))
  const suffix =
    usage.level === 'blocked'
      ? 'next request may be rejected'
      : usage.level === 'compact'
        ? 'auto-compact on the next turn'
        : 'auto-compact coming up'
  return 'Context ' + String(percent) + '% · ' + suffix
}
