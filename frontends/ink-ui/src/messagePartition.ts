import type { ChatMessage } from './protocol.js'

export type MessagePartition = {
  staticMessages: ChatMessage[]
  activeMessages: ChatMessage[]
}

/**
 * Split transcript into terminal scrollback (static) vs live viewport (active).
 *
 * Single-channel rule:
 * - done → Static only (never re-enter live)
 * - !done → live only (open-turn user + streaming assistant)
 *
 * P0-A running visibility: open-turn user is !done so it paints in live until
 * finalize, avoiding premature Static writes that the growing live dock can cover.
 */
export function splitStaticAndActiveMessages(
  messages: ChatMessage[],
  options: { keepLatestTaskActive?: boolean } = {},
): MessagePartition {
  let latestTaskId: number | undefined
  for (let index = messages.length - 1; index >= 0; index--) {
    const taskId = messages[index]?.taskId
    if (taskId !== undefined) {
      latestTaskId = taskId
      break
    }
  }

  const staticMessages = messages.filter(message => message.done)
  if (latestTaskId === undefined) {
    return { staticMessages, activeMessages: [] }
  }

  const hasPendingMessage = messages.some(
    message => message.taskId === latestTaskId && !message.done,
  )
  const holdActive = hasPendingMessage || Boolean(options.keepLatestTaskActive)
  if (!holdActive) {
    return { staticMessages, activeMessages: [] }
  }

  const activeMessages = messages.filter(
    message => message.taskId === latestTaskId && !message.done,
  )
  return { staticMessages, activeMessages }
}
