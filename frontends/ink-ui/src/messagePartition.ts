import type { ChatMessage } from './protocol.js'

export type MessagePartition = {
  staticMessages: ChatMessage[]
  activeMessages: ChatMessage[]
}

export function splitStaticAndActiveMessages(messages: ChatMessage[]): MessagePartition {
  let latestTaskId: number | undefined
  for (let index = messages.length - 1; index >= 0; index--) {
    const taskId = messages[index]?.taskId
    if (taskId !== undefined) {
      latestTaskId = taskId
      break
    }
  }
  if (latestTaskId === undefined) {
    return { staticMessages: messages.filter(message => message.done), activeMessages: [] }
  }

  const activeStart = messages.findIndex(message => message.taskId === latestTaskId)
  if (activeStart === -1) {
    return { staticMessages: messages.filter(message => message.done), activeMessages: [] }
  }

  const beforeActive = messages.slice(0, activeStart)
  const activeMessages = messages.slice(activeStart)
  return {
    staticMessages: beforeActive.filter(message => message.done),
    activeMessages,
  }
}
