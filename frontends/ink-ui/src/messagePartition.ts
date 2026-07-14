import type { ChatMessage } from './protocol.js'

export type MessagePartition = {
  staticMessages: ChatMessage[]
  activeMessages: ChatMessage[]
}

/**
 * Split transcript into terminal scrollback (static) vs live viewport (active).
 *
 * Codex-aligned rule: done messages commit to scrollback; only !done messages
 * stay in the live ring. A done user prompt must never re-enter active, or
 * Ink <Static> + live will double-render the same user line.
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

  // Live only carries in-flight content (streaming assistant, etc.).
  const activeMessages = messages.filter(
    message => message.taskId === latestTaskId && !message.done,
  )
  return { staticMessages, activeMessages }
}
