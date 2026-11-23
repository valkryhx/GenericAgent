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
  previous?: MessagePartition,
): MessagePartition {
  const firstOpenIndex = messages.findIndex(message => !message.done)
  const splitAt = firstOpenIndex === -1 ? messages.length : firstOpenIndex
  const prefix = previous?.staticMessages
  return {
    staticMessages: prefix?.length === splitAt && prefix.every((message, index) => message === messages[index])
      ? prefix
      : messages.slice(0, splitAt),
    activeMessages: messages.slice(splitAt),
  }
}
