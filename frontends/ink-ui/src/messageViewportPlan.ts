export type MessageViewportPlan =
  | { kind: 'none' }
  | { kind: 'ready' }
  | { kind: 'live'; height: number }

export function planMessageViewport(input: { hasStaticMessages: boolean; liveLineCount: number; messageRows: number }): MessageViewportPlan {
  if (input.liveLineCount > 0) {
    return { kind: 'live', height: Math.max(1, Math.floor(input.messageRows)) }
  }
  if (!input.hasStaticMessages) {
    return { kind: 'ready' }
  }
  return { kind: 'none' }
}
