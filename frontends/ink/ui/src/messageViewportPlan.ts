export type MessageViewportPlan =
  | { kind: 'none' }
  | { kind: 'ready'; height: number }
  | { kind: 'live'; height: number }

/** Default cap for streaming live rows (Codex-like: dock grows then stops). */
export const DEFAULT_MAX_LIVE_ROWS = 12

/**
 * Plan the live message slot above bottom chrome.
 *
 * Codex content-desired semantics (phase 2):
 * - idle + Static history → no tall spacer (`none`); composer sticks near history tail
 * - ready (no history) → compact 1-row ready cue
 * - streaming → height follows live line count, capped by maxLiveRows and messageRows
 *
 * Phase-1 "always messageRows" was an overfix that pushed composer to the terminal
 * bottom with a large empty gap above it.
 */
export function planMessageViewport(input: {
  hasStaticMessages: boolean
  liveLineCount: number
  messageRows: number
  maxLiveRows?: number
  /** When true (running/stopping), keep a 1-row live cue even if no stream lines yet. */
  keepLivePlaceholder?: boolean
}): MessageViewportPlan {
  const room = Math.max(1, Math.floor(input.messageRows))
  const maxLive = Math.max(1, Math.floor(input.maxLiveRows ?? DEFAULT_MAX_LIVE_ROWS))
  const liveCount = Math.max(0, Math.floor(input.liveLineCount))

  if (liveCount > 0) {
    const height = Math.min(liveCount, maxLive, room)
    return { kind: 'live', height }
  }
  if (input.keepLivePlaceholder) {
    // Running with no assistant lines yet: still reserve a short live band so
    // open-turn user / thinking cue stays in the dock (visibility P0-A).
    return { kind: 'live', height: 1 }
  }
  if (!input.hasStaticMessages) {
    return { kind: 'ready', height: 1 }
  }
  // Idle with scrollback history: no empty live spacer — stick composer to Static tail.
  return { kind: 'none' }
}
