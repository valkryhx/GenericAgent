import type { ChatMessage } from './protocol.js'

export type MessagePartition = {
  staticMessages: ChatMessage[]
  activeMessages: ChatMessage[]
}

/**
 * Split transcript into terminal scrollback (static) vs live viewport (active).
 *
 * Ink `<Static>` only ever *appends* by `items.length`. If a later render inserts
 * newly-done rows *before* already-printed static rows, Static re-emits the tail —
 * classic symptom: mid-run `/stop` prints once, then `assistant_done` finalizes the
 * open turn and a second gray `Stop requested` appears (often without a second `/stop`).
 *
 * Contract: `staticMessages` is always a strict prefix of the transcript.
 * - done prefix → Static
 * - from the first `!done` message through the end → live (including later done
 *   local-command rows such as `/stop` / `Stop requested`)
 * - all done → Static only
 *
 * P0-A running visibility: open-turn user is `!done`, so it stays live until finalize.
 */
export function splitStaticAndActiveMessages(
  messages: ChatMessage[],
  _options: { keepLatestTaskActive?: boolean } = {},
): MessagePartition {
  const firstOpenIndex = messages.findIndex(message => !message.done)
  if (firstOpenIndex === -1) {
    return {
      staticMessages: messages.filter(message => message.done),
      activeMessages: [],
    }
  }

  return {
    staticMessages: messages.slice(0, firstOpenIndex),
    activeMessages: messages.slice(firstOpenIndex),
  }
}
