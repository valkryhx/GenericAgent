export function inputDivider(width = 64): string {
  return '─'.repeat(Math.max(0, Math.floor(width)))
}

export function inputPrompt(input: string): string {
  return `> ${input}`
}
