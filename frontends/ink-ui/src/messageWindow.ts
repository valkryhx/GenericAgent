import type { ChatMessage } from './protocol.js'

export function visibleMessages(messages: ChatMessage[], maxMessages = 80): ChatMessage[] {
  return messages.slice(-maxMessages)
}

export function tailLines(text: string, maxLines: number): string {
  const lines = (text || '').split('\n')
  if (lines.length <= maxLines) return text
  const omitted = lines.length - maxLines
  return [`... ${omitted} earlier lines omitted ...`, ...lines.slice(-maxLines)].join('\n')
}
