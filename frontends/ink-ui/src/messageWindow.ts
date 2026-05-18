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

export function assistantDisplayText(
  text: string,
  options: {
    expanded: boolean
    done: boolean
    maxExpandedLines?: number
  },
): string {
  if (options.expanded) {
    return tailLines(text, options.maxExpandedLines ?? (options.done ? 80 : 40))
  }
  return tailLines(text, options.done ? 30 : 18)
}
